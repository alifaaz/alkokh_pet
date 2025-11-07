### Pet App

A comprehensive Frappe app for managing pets with full REST API support.

## Features

- ✅ Complete CRUD operations for pet management
- ✅ Medical records and vaccination tracking
- ✅ Adoption management
- ✅ 16 REST API endpoints with Swagger UI documentation
- ✅ Automatic CSRF token handling
- ✅ Token-based authentication
- ✅ Advanced search and filtering

## API Documentation

Access the interactive API documentation at:
```
http://your-site-url/api-docs
```

**Quick API Overview:**
- **16 Total Endpoints**: Full CRUD + specialized queries
- **Automatic CSRF**: Swagger UI handles CSRF tokens automatically
- **Token Auth**: Use API key + secret for authentication

See [AUTHENTICATION_GUIDE.md](AUTHENTICATION_GUIDE.md) for detailed authentication and CSRF instructions.



### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app pet_app
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/pet_app
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
