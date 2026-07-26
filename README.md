# python-ppa

`python-ppa` is a lightweight, Spring Data ppa-inspired persistence framework for MongoDB and PostgreSQL built on top of **Python 3.13+**, **Pydantic v2**, **PyMongo**, and **psycopg**. 

It uses advanced Python metaprogramming (`metaclass`) to dynamically generate MongoDB queries at runtime based on method naming conventions or explicit query declarations, drastically reducing boilerplate code.

---

## 🚀 Features

* **ppa-Like Repository Pattern:** Declare an interface, and let the metaclass handle query assembly dynamically.
* **Query Naming Conventions:** Instantly supports `find_by_*`, `find_all_by_*`, `exists_by_*`, and `count_by_*` pattern resolutions.
* **Custom Query Annotations:** Bind complex MongoDB or PostgreSQL query templates with custom parameter injections using the `@query` decorator.
* **Data Validation:** Fully powered by Pydantic v2 for robust runtime type checking and parsing.
* **Zero-Boilerplate ID Mapping:** Automatically handles conversions between stringified hexadecimal keys and native BSON `ObjectId` footprints.
* **Environment Profiles:** Flexible configurations handling multi-stage deployment environments (`dev`, `prod`, etc.) via YAML interpolation.

---

## 📁 Directory Structure

```text
.
├── README.md
├── ppa
│   ├── __init__.py
│   ├── config.py         # App bootstrapping, YAML parsing & environment mapping
│   ├── mongo
│       ├── __init__.py
│       └── interface.py  # Repository core interface, custom metadata, and queries
│   └── postgres
│       ├── __init__.py
│       └── interface.py  # PostgreSQL repository interface and SQL query conventions
├── pyproject.toml
└── requirements.txt
```

## 🛠️ Getting Started

### Prerequisites

* Python 3.13 or higher

### Installation & Virtual Env Setup

1. Clone the repository and navigate to its root: `cd python-ppa`
2. Spin up a virtual environment and update your pip core dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

3. Install the project library dependencies:

```bash
pip install -r requirements.txt
```

## ⚙️ Configuration Management

The framework supports multi-profile YAML configurations with real-time environment substitution syntax (e.g., ${ENV_VAR:default_value}).
Create a `resources/` directory at the root of your execution workspace and include your application sheets.

1. Master Configuration (`resources/settings.yml`)

```yaml
app:
  profile: ${APP_PROFILE:dev} # Swaps profile to target environment configuration
```

2. Environment Profile Configuration (`resources/settings-dev.yml`)

```yaml
mongodb:
  uri: ${MONGO_URI:mongodb://localhost:27017}
  database: ${MONGO_DB_NAME:ppa_database}

# Or configure PostgreSQL instead.
postgresql:
  dsn: ${POSTGRES_DSN:postgresql://postgres:postgres@localhost:5432/ppa_database}

# Equivalent field-based PostgreSQL configuration:
# postgresql:
#   host: ${POSTGRES_HOST:localhost}
#   port: ${POSTGRES_PORT:5432}
#   database: ${POSTGRES_DB_NAME:ppa_database}
#   user: ${POSTGRES_USER:postgres}
#   password: ${POSTGRES_PASSWORD:postgres}

framework:
  logging:
    enabled: true
    level: "DEBUG"
```

## PostgreSQL Quickstart

The `demo/` folder contains a complete local PostgreSQL verification app.

1. Start PostgreSQL:

```bash
cd demo
docker compose up -d
```

2. Run the demo from the repository root:

```bash
./venv/bin/python demo/main.py
```

The demo creates a `users` table and verifies insert, convention queries, a custom SQL query, update, and delete through `ppa.postgres`.

For your own app, create `resources/settings.yml`:

```yaml
app:
  profile: ${APP_PROFILE:dev}
```

Then create `resources/settings-dev.yml`:

```yaml
postgresql:
  host: ${POSTGRES_HOST:localhost}
  port: ${POSTGRES_PORT:5432}
  database: ${POSTGRES_DB_NAME:ppa_demo}
  user: ${POSTGRES_USER:ppa_user}
  password: ${POSTGRES_PASSWORD:ppa_password}

framework:
  logging:
    enabled: true
    level: DEBUG
```

You can also use a single DSN:

```yaml
postgresql:
  dsn: ${POSTGRES_DSN:postgresql://ppa_user:ppa_password@localhost:5432/ppa_demo}
```

Create the database table before using the repository. `python-ppa` currently maps models to existing tables; it does not run migrations for you.

```sql
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    age INTEGER NOT NULL
);
```

Define a PostgreSQL-backed entity and repository:

```python
from ppa.postgres import EntityModel, IRepository, query, table

@table(name="users")
class User(EntityModel):
    username: str
    email: str
    age: int

class UserRepository(IRepository[User]):
    def find_by_username(self, username: str) -> User | None: ...
    def exists_by_username(self, username: str) -> bool: ...
    def count_by_age(self, age: int) -> int: ...
    def find_all_by_age(self, age: int) -> list[User]: ...

    @query("SELECT * FROM users WHERE email = ?0 AND age >= ?1")
    def find_by_email_and_min_age(self, email: str, min_age: int) -> list[User]: ...
```

Use the repository:

```python
from ppa.config import close_db_connection

repo = UserRepository()

user_id = repo.save(User(username="pratyush", email="pratyush@example.com", age=25))
user = repo.find_by_username("pratyush")

print(user.model_dump() if user else None)
print(repo.exists_by_username("pratyush"))
print(repo.count_by_age(25))
print(repo.find_by_email_and_min_age("pratyush@example.com", 21))

repo.update(user_id, {"age": 26})
repo.delete(user_id)

close_db_connection()
```

## MongoDB Usage Example

Here is a quick overview of how you can configure a domain entity and generate auto-implemented interface pipelines:

1. Define your Document Model

```python
from pydantic import Field
from ppa.mongo.interface import DocumentModel, document

@document(name="users")
class User(DocumentModel):
    id: str = Field(alias="_id", default=None)
    username: str
    email: str
    age: int
```

2. Declare your Interface Repository

By extending IRepository[T], method naming patterns are captured and converted into database interactions seamlessly.

```python
from ppa.mongo.interface import IRepository, query
from typing import List, Optional

class UserRepository(IRepository[User]):
    
    # 1. Query generation by structural method naming convention
    def find_by_username(self, username: str) -> Optional[User]: ...
    
    def count_by_age(self, age: int) -> int: ...

    # 2. Templated declaration using placeholder substitutions
    @query(definition={"email": "?0", "age": {"$gte": "?1"}})
    def find_by_email_and_min_age(self, email: str, min_age: int) -> List[User]: ...
```

3. Execute CRUD Actions

```python
from ppa.config import close_db_connection

# Initialize repository instance
user_repo = UserRepository()

# Save a document
new_user = User(username="pratyush", email="pratyush@example.com", age=25)
user_id = user_repo.save(new_user)

# Fetch using automatic query generation
user = user_repo.find_by_username("pratyush")
print(f"Found User: {user.email if user else 'Not Found'}")

# Clean up connections on process termination
close_db_connection()
```

## 📜 License

This project is open-source software licensed under the MIT License.
