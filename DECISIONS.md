# Technical Decisions

### 1. Indexes
I added a unique index on `employees.emp_code` to prevent duplicate employee codes. I also added a unique compound index on `attendance_logs` using `emp_code` and `date`. This keeps one attendance record per employee per day and helps queries find records quickly. The indexes are created when the application starts.

### 2. Punch-in race
If two punch-in requests arrive for the same employee and date at almost the same time, both may try to create a record. The unique index prevents both records from being stored. One request should succeed, while the duplicate request should return a conflict response (`409`). This depends on the duplicate-key error being handled correctly in the endpoint.

### 3. Leaderboard ties
When employees have the same total late minutes, they should receive the same rank. For example, the ranks can be 1, 2, 2, and 4. The employee code provides a consistent order when two employees have equal totals. The limit should be applied after ranking so tied results are handled correctly.

### 4. Employees without logs
Department headcount should include employees who joined on or before the end of the selected month, even if they have no attendance records. Missing attendance records should not automatically be counted as absences.

### 5. Scaling
If the dataset became 100 times larger, I would first check query execution statistics and identify slow aggregation stages. I would then review indexes and test performance with a larger dataset before deciding what needs to change.
