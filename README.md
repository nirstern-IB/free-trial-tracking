# Free trial tracking

A Flask dashboard for viewing free trial build activity from Snowflake.
`app.py` is the application; `snowflake_connection.py` is a connection helper,
not a separate service.

## Install

Use Python 3.11 or newer. On Debian 12, install the interpreter, virtual
environment support, and pip before creating the environment:

```sh
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
```

## Build

```sh
.venv/bin/python -m build --sdist --outdir dist/free-trial-tracking
```

This creates a source distribution containing the application, HTML templates,
dependency lists, and tests. Extract it and follow the same install procedure
to run from source. It is not a wheel or a standalone executable.

## Test

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The tests use Flask's in-process test client and mock database connections.
They do not start a server, contact Snowflake, or require environment settings
or credentials. Build and install also require no application settings.

## Run locally

```sh
.venv/bin/python app.py
```

The development server listens on localhost port 5050. The default login uses
interactive Snowflake browser authentication. For token authentication, supply
`SNOWFLAKE_TOKEN` and `SNOWFLAKE_USER` securely in the process environment.
`FLASK_SECRET_KEY` optionally supplies the session signing secret; otherwise
the application generates a new one on process startup. Never commit secrets.
The development server enables debugging and is intended for local use.
