# Employee Attendance & Analytics API

## Candidate Kit

Read the following files in order:

1. `PROBLEM_STATEMENT.docx` — assignment requirements and evaluation criteria.
2. `openapi.yaml` — API contract and business rules (R1–R10).
3. `DATA_MODEL.md` — MongoDB collections, field types, and sample documents.
4. `app/main.py` — application code. All application code is kept in this file.
5. `REVIEW.md` and `DECISIONS.md` — code review findings and technical decisions.

The `sample_data/` directory contains sample MongoDB Extended JSON documents, and `sample_seed.py` loads the sample data. The evaluation dataset may contain additional edge cases and more records than the provided sample.

## Tech Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo
- Uvicorn

## Local Setup

Make sure MongoDB is running before starting the application.

Create and activate a virtual environment.

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS:**

```bash
python -m venv .venv
source .venv/bin/activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

Configure the following environment variables:

- `MONGO_URI` — MongoDB connection URI.
- `MONGO_DB` — database name.

For local development, these values can be stored in a `.env` file in the project root if the application loads it. Do not commit this file or any credentials to the repository.

To load the provided sample data, run:

```bash
python sample_seed.py
```

Run this command only when you want to load the sample dataset into your configured database.

## Run the Application

From the project root, run:

```bash
python -m uvicorn app.main:app --port 8000
```

The API documentation is available at:

`http://127.0.0.1:8000/docs`

The health endpoint is available at:

`http://127.0.0.1:8000/health`

## Features

- Employee creation and listing.
- Attendance punch-in and punch-out.
- Attendance regularization.
- Monthly employee attendance analytics.
- Department attendance summaries.
- Late-arrival leaderboard.
- Department attendance trends.
- Query execution plans through the admin explain endpoint.

## Implementation Notes

The application reads MongoDB configuration from environment variables and creates its required indexes at startup. Attendance analytics are implemented using MongoDB aggregation pipelines.

The provided sample dataset is useful for local testing, but additional edge cases should also be tested before considering the implementation complete.

## Submission

The required submission files are:

- `app/main.py`
- `requirements.txt`
- `REVIEW.md`
- `DECISIONS.md`
- `README.md`

Do not include `.env` files, secrets, Dockerfiles, virtual environments, or data dumps in the public repository.
