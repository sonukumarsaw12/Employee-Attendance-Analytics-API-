# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | The endpoint reports success without checking whether MongoDB is reachable. | Stop MongoDB and call `GET /health`. | Added a database ping and proper error handling. |
| 2 | `create_employee()` | Checking for duplicate employee codes before insertion does not prevent concurrent requests from creating duplicates. | Send two requests with the same employee code simultaneously. | Added a unique index and handled duplicate-key errors. |
| 3 | `list_employees()` | The pagination offset skips the first page when page numbering starts at 1. | Request page 1 and check whether the first records are returned. | Changed the offset to `(page - 1) * page_size`. |
| 4 | `list_employees()` | The total count does not apply the same filters as the employee query. | Filter by department and compare the count with the matching records. | Applied the same filter to the count query. |
| 5 | `list_employees()` | Results do not have a consistent sort order before pagination. | Request the same page repeatedly and compare the record order. | Added sorting by employee code. |
| 6 | `compute_late_minutes()` | The grace-period boundary can be handled incorrectly because elapsed time is truncated to whole minutes. | Test punches immediately before and after the grace-period boundary. | Compare elapsed seconds with the grace-period threshold before calculating late minutes. |
| 7 | `compute_late_minutes()` | The calculation does not correctly handle shifts crossing midnight. | Test a shift from 22:00 to 06:00 with a punch after midnight. | Added overnight-shift handling and IST-aware time calculations. |
| 8 | `compute_work_hours()` | Built-in `round()` uses ties-to-even rounding, which may not match the required rounding rule. | Test a duration whose calculated hours fall exactly on a rounding boundary. | Used `Decimal` with `ROUND_HALF_UP`. |
| 9 | `compute_overtime()` | Any positive time after shift end can be counted as overtime, even when it is below the required minimum. | Test a punch-out shortly after the scheduled shift end. | Applied the 30-minute overtime threshold. |
| 10 | `compute_overtime()` | The calculation assumes the shift end and punch-out fall on the same date. | Test a shift that ends after midnight. | Added support for overnight shifts. |
| 11 | `punch_in()` | An unknown employee is not handled safely before attendance details are calculated. | Submit a punch-in request for a nonexistent employee code. | Added an employee lookup and a `404 Not Found` response. |
| 12 | `punch_in()` | Using the machine's local time can cause inconsistent attendance dates across environments. | Test a timestamp near midnight on a machine using a different time zone. | Standardised timestamp handling around UTC and used IST for attendance dates. |
| 13 | `punch_in()` | A timestamp of `0` is treated as missing by a truthiness check and replaced with the current time. | Submit a request with `punched_at: 0`. | Distinguished missing values from supplied values and added timestamp validation. |
| 14 | `punch_in()` | Unsupported attendance status values can be accepted. | Submit a request with an invalid status string. | Restricted the request to supported status values. |
| 15 | `punch_in()` | An application-level duplicate check cannot guarantee one record per employee per day during concurrent requests. | Send simultaneous punch-in requests for the same employee and date. | Added a unique compound index on `(emp_code, date)` and handled duplicate-key errors. |
| 16 | Response serialization | Returning database documents directly can expose internal MongoDB identifiers or return values in an inconsistent format. | Inspect the JSON returned by employee and attendance endpoints. | Added response conversion functions and consistent timestamp serialization. |
| 17 | `list_attendance()` | Fetching records into Python before sorting and pagination creates unnecessary work as the collection grows. | Compare the number of fetched records with the requested page size. | Moved filtering, sorting, and pagination into MongoDB. |
| 18 | Employee and attendance list endpoints | Pagination inputs are not adequately bounded. | Try page 0, a negative page number, or an excessive page size. | Added validation and limits for pagination parameters. |
| 19 | Application startup | The original implementation does not create the indexes needed for uniqueness and common queries. | Start with an empty database and inspect the collection indexes. | Added index creation during application startup. |

## Things I checked and decided were not defects

- **Helper functions:** Keeping attendance calculations in separate helpers makes them easier to test and maintain. I retained this structure.
- **MongoDB:** The database choice fits the assignment's employee and attendance data, so I kept it.
- **API structure:** The existing FastAPI structure was a reasonable starting point. I extended it instead of replacing the framework.
- **Scope of review:** I counted defects in the original starter code, not missing functionality from the new endpoints required in Part B.
