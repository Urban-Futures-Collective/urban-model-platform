# Database docs 
(scribble format for now)

* [Dockerfile](../../../Dockerfile) refers to `alembic.ini` and `migrations`
* `docker-compose` files include `api-db` service (renamed to `ump-db`)
* local env setup as in UMP's [quickstart](https://urban-model-platform.github.io/urban-model-platform/content/01-intro/quick_start.html) and  [contributing guide](https://github.com/Urban-Model-Platform/urban-model-platform/blob/v3.0.0alpha/base/CONTRIBUTING.md);
* then use [alembic](https://alembic..org/en/latest/)'s `.mako` file to generate a new (0003) file in `migrations/versions/`, describing database updates (implementation of additional tables & fields, as per [diagram](https://github.com/Urban-Futures-Collective/urban-model-platform/blob/v3.0.0alpha/user-management-db/docs/diagrams/model-registry-uml.mmd)), with [sqlalchemy](https://www.sqlalchemy.org) 

***

https://github.com/Urban-Model-Platform/urban-model-platform/issues/115 >> hiccup occurs again 