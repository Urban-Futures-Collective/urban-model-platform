# Database docs 
(scribble format for now)

* [Dockerfile](../../../Dockerfile) refers to `alembic.ini` and `migrations`
* `docker-compose` files include `api-db` service (renamed to `ump-db`)
* local env setup as in UMP's [contributing guide](https://github.com/Urban-Model-Platform/urban-model-platform/blob/v3.0.0alpha/base/CONTRIBUTING.md);
* then: `migrations/versions/` gets a new file (0003), describing database updates (implementation of additional tables & fields, as per [diagram](https://github.com/Urban-Futures-Collective/urban-model-platform/blob/v3.0.0alpha/user-management-db/docs/diagrams/model-registry-uml.mmd)), with [sqlalchemy](https://www.sqlalchemy.org) & [alembic](https://alembic..org/en/latest/)