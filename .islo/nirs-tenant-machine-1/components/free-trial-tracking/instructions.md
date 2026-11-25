# Free trial tracking

Run `islo components install`, `islo components build`, and `islo components test`
to install the documented Python dependencies, create the source distribution in
`dist/free-trial-tracking/`, and run the offline tests.

Run `islo components start` to start the documented `app.py` development server
at http://127.0.0.1:5050. Runtime entrypoints default `SAMPLE_DATA` to `1`, preserving
environment overrides. Bundled synthetic data needs no Snowflake credentials.
Open the dashboard, choose a license filter from a displayed build, and confirm
that the resulting rows match that license. `islo components start-verification`
reports the same address as a verification target. Health checks the dashboard
sample badge and a successful build-summary query.

Use `islo components status` and `islo components stop` to inspect and stop it.
The local development server stays on localhost; no web Surface is published.
Runtime PID and log files live under the system temporary directory in
`islo-nirs-tenant-machine-1-free-trial-tracking/`.
