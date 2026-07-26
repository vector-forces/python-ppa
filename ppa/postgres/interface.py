import inspect
import re
from typing import Any, Callable, ClassVar, Generic, List, Optional, TypeVar, cast, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict

from ppa.config import get_framework_logger, get_postgres_connection

T = TypeVar("T", bound="EntityModel")
F = TypeVar("F", bound=Callable[..., Any])
framework_logger = get_framework_logger("postgres.repository")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EntityModel(BaseModel):
    """Base class for PostgreSQL-backed models."""

    __table_name__: ClassVar[str]

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    id: Optional[int] = None

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)


class EntityList(list[T]):
    """List of entities that can be used to perform bulk operations."""

    def model_dump(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [entity.model_dump(**kwargs) for entity in self]

    @classmethod
    def model_validate(cls, obj: list[Any], entity_cls: type[T]) -> "EntityList[T]":
        if not isinstance(obj, list) and not hasattr(obj, "__iter__"):
            raise ValueError("Data provided to EntityList.model_validate must be iterable.")

        return cls([entity_cls.model_validate(item) for item in obj])


def query(definition: str) -> Callable[[F], F]:
    """Annotation to specify a custom PostgreSQL query template with ?0 style placeholders."""

    def decorator(func: F) -> F:
        func.__query_template__ = definition
        return func

    return decorator


def table(name: str) -> Callable[[type[EntityModel]], type[EntityModel]]:
    """Class decorator to bind a Pydantic model to a PostgreSQL table."""

    _quote_identifier(name)

    def decorator(cls: type[EntityModel]) -> type[EntityModel]:
        cls.__table_name__ = name
        return cls

    return decorator


def _quote_identifier(identifier: str) -> str:
    if not IDENTIFIER_PATTERN.match(identifier):
        raise ValueError(f"Invalid SQL identifier: {identifier}")
    return f'"{identifier}"'


def _field_names(entity_cls: type[EntityModel]) -> set[str]:
    return set(entity_cls.model_fields.keys())


def _build_where_clause(field_name: str) -> str:
    return f"{_quote_identifier(field_name)} = %s"


def _custom_query_to_sql(template: str, ordered_args: list[Any]) -> tuple[str, list[Any]]:
    values: list[Any] = []

    def replace(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        try:
            values.append(ordered_args[idx])
        except IndexError as exc:
            raise ValueError(f"Missing argument for SQL placeholder ?{idx}") from exc
        return "%s"

    return re.sub(r"\?(\d+)", replace, template), values


class RepositoryMeta(type):
    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]):
        cls = super().__new__(mcs, name, bases, namespace)

        if name == "IRepository":
            return cls

        entity_cls = mcs._extract_entity_cls(cls)
        if not entity_cls:
            return cls

        typed_entity_cls = cast(type[EntityModel], entity_cls)

        if not hasattr(typed_entity_cls, "__table_name__"):
            raise TypeError(
                f"RepositoryDefinitionError: The model '{entity_cls.__name__}' "
                f"used by '{name}' must be annotated with @table(name='...')"
            )

        cls._entity_cls = typed_entity_cls
        valid_fields = _field_names(typed_entity_cls)

        for attr_name, attr_value in list(namespace.items()):
            if inspect.isfunction(attr_value) and not attr_name.startswith("_"):
                query_template = getattr(attr_value, "__query_template__", None)
                is_convention = re.match(r"(find_by|find_all_by|exists_by|count_by)_(.*)", attr_name)

                if not is_convention and not query_template:
                    framework_logger.info("Convention does not exist, relying on user implementation")
                    continue

                hints = get_type_hints(attr_value)
                return_type = hints.get("return")

                if isinstance(query_template, str):
                    generated_method = mcs._build_annotated_method(
                        attr_name,
                        attr_value,
                        query_template,
                        return_type,
                        typed_entity_cls,
                    )
                elif is_convention is not None:
                    generated_method = mcs._build_convention_method(
                        attr_name,
                        is_convention,
                        attr_value,
                        valid_fields,
                        return_type,
                        typed_entity_cls,
                    )
                else:
                    continue

                setattr(cls, attr_name, generated_method)

        return cls

    @staticmethod
    def _extract_entity_cls(cls: type) -> type[BaseModel] | None:
        for base in getattr(cls, "__orig_bases__", []):
            if get_origin(base) is IRepository:
                args = get_args(base)
                if args and issubclass(args[0], BaseModel):
                    return args[0]
        return None

    @staticmethod
    def _build_annotated_method(
        name: str,
        func: Callable[..., Any],
        template: str,
        return_type: Any,
        entity_cls: type[EntityModel],
    ) -> Callable[..., Any]:
        sig = inspect.signature(func)
        param_names = [p for p in sig.parameters if p != "self"]

        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(self, *args, **kwargs)
            bound.apply_defaults()
            ordered_args = [bound.arguments[p_name] for p_name in param_names if p_name in bound.arguments]
            sql, params = _custom_query_to_sql(template, ordered_args)

            framework_logger.debug(
                "Invoked custom Postgres query %s.%s with args=%s kwargs=%s",
                self.__class__.__name__,
                name,
                args,
                kwargs,
            )

            return execute_dynamic_query(sql, params, "custom", return_type, entity_cls)

        wrapper.__name__ = name
        wrapper.__qualname__ = f"{entity_cls.__name__}Repository.{name}"
        return wrapper

    @staticmethod
    def _build_convention_method(
        name: str,
        match: re.Match[str],
        func: Callable[..., Any],
        valid_fields: set[str],
        return_type: Any,
        entity_cls: type[EntityModel],
    ) -> Callable[..., Any]:
        prefix, field_name = match.groups()

        if field_name not in valid_fields:
            raise TypeError(
                f"RepositoryDefinitionError: Field '{field_name}' does not exist on model {entity_cls.__name__}"
            )

        sql_field = _quote_identifier(field_name)
        table_name = _quote_identifier(entity_cls.__table_name__)
        sig = inspect.signature(func)
        param_names = [p for p in sig.parameters if p != "self"]

        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            bound = sig.bind_partial(self, *args, **kwargs)
            bound.apply_defaults()
            actual_value = bound.arguments.get(param_names[0]) if param_names else None

            if prefix == "count_by":
                sql = f"SELECT COUNT(*) AS count FROM {table_name} WHERE {sql_field} = %s"
            elif prefix == "exists_by":
                sql = f"SELECT EXISTS(SELECT 1 FROM {table_name} WHERE {sql_field} = %s LIMIT 1) AS exists"
            else:
                sql = f"SELECT * FROM {table_name} WHERE {sql_field} = %s"

            framework_logger.debug(
                "Invoked convention Postgres query %s.%s using %s=%r",
                self.__class__.__name__,
                name,
                field_name,
                actual_value,
            )

            return execute_dynamic_query(sql, [actual_value], prefix, return_type, entity_cls)

        wrapper.__name__ = name
        wrapper.__qualname__ = f"{entity_cls.__name__}Repository.{name}"
        return wrapper


def execute_dynamic_query(
    sql: str,
    params: list[Any],
    strategy: str,
    return_type: Any,
    entity_cls: type[EntityModel],
) -> Any:
    framework_logger.debug("Executing PostgreSQL query with strategy [%s]: %s", strategy, sql)
    origin_type = get_origin(return_type) or return_type

    with get_postgres_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)

            if strategy == "count_by" or origin_type is int:
                row = cursor.fetchone()
                return int(row["count"]) if row else 0

            if strategy == "exists_by" or origin_type is bool:
                row = cursor.fetchone()
                return bool(row["exists"]) if row else False

            if origin_type in (list, EntityList):
                rows = cursor.fetchall()
                results = [entity_cls.model_validate(row) for row in rows]
                return EntityList(results) if origin_type is EntityList else results

            row = cursor.fetchone()
            return entity_cls.model_validate(row) if row else None


class IRepository(Generic[T], metaclass=RepositoryMeta):
    def __init__(self):
        entity_cls = cast(type[EntityModel], self._entity_cls)
        self.table_name = entity_cls.__table_name__
        framework_logger.debug(
            "Initialized repository [%s] for table [%s]",
            self.__class__.__name__,
            self.table_name,
        )

    def save(self, pyd_model: T) -> str:
        data = pyd_model.model_dump()
        data.pop("id", None)
        if not data:
            raise ValueError("No data provided for insert")

        columns = list(data.keys())
        quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO {_quote_identifier(self.table_name)} ({quoted_columns}) VALUES ({placeholders}) RETURNING id"

        with get_postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, [data[column] for column in columns])
                row = cursor.fetchone()
                return str(row["id"]) if row and "id" in row else ""

    def save_all(self, pyd_models: List[T]) -> List[str]:
        return [self.save(pyd_model) for pyd_model in pyd_models]

    def find_all(self) -> EntityList[T]:
        sql = f"SELECT * FROM {_quote_identifier(self.table_name)}"
        with get_postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
                return EntityList([self._entity_cls.model_validate(row) for row in rows])

    def find(self, query_dict: dict[str, Any]) -> T:
        if not query_dict:
            raise ValueError("Query criteria must be provided")

        clauses = [_build_where_clause(field_name) for field_name in query_dict]
        sql = f"SELECT * FROM {_quote_identifier(self.table_name)} WHERE {' AND '.join(clauses)} LIMIT 1"

        with get_postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, list(query_dict.values()))
                row = cursor.fetchone()
                if not row:
                    raise ValueError("No data found")
                return self._entity_cls.model_validate(row)

    def delete(self, resource_id: str) -> bool:
        sql = f"DELETE FROM {_quote_identifier(self.table_name)} WHERE {_quote_identifier('id')} = %s"
        with get_postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, [resource_id])
                return cursor.rowcount > 0

    def update(self, resource_id: str, data: dict[str, Any]) -> bool:
        update_data = {key: value for key, value in data.items() if key != "id"}
        if not update_data:
            raise ValueError("No data provided for update")

        assignments = ", ".join(f"{_quote_identifier(field_name)} = %s" for field_name in update_data)
        sql = f"UPDATE {_quote_identifier(self.table_name)} SET {assignments} WHERE {_quote_identifier('id')} = %s"

        with get_postgres_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, [*update_data.values(), resource_id])
                return cursor.rowcount > 0
