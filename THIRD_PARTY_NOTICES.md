# Third-party notices

IssuePilot's project-authored source code is licensed under the MIT License in
`LICENSE`. That license does not replace or relicense third-party packages,
their source code, or third-party data.

The project does not vendor the packages below into its source tree. They are
installed from Python package distributions by `requirements.txt` or
`requirements.docker.lock`. The following identifiers were read from the
metadata of the locally installed, locked versions during the 2026-10-28
release audit:

| Package | Locked version | Declared license metadata |
|---|---:|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.10.19 | MPL-2.0 |
| click | 8.5.0 | BSD-3-Clause |
| colorama | 0.4.6 | BSD License classifier |
| distro | 1.9.0 | Apache License 2.0 |
| fastapi | 0.153.0 | MIT |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| jiter | 0.17.0 | MIT |
| openai | 2.54.0 | Apache-2.0 |
| opentelemetry-api | 1.45.1 | Apache-2.0 |
| pydantic | 2.14.1 | MIT |
| pydantic-core | 2.50.1 | MIT |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| starlette | 1.9.0 | BSD-3-Clause |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| typing-inspection | 0.4.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |

Downstream distributors must comply with each dependency's license and retain
the notices and license files supplied in its distribution. In particular,
Apache-2.0, MPL-2.0, PSF-2.0, BSD, and MIT terms continue to apply to their
respective packages. This table is an inventory, not a grant of rights or a
substitute for the packages' complete license texts.

`data/issues_demo.json` contains synthetic examples authored for IssuePilot.
The private local research corpus of real GitHub Issues is not part of the
release manifest. IssuePilot's MIT License does not cover third-party GitHub
Issue text collected by a user.
