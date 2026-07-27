# --workers 1 is REQUIRED: jobs live in an in-memory dict. Multiple workers =
# the job exists in one process while the poll request lands in another →
# phantom "job not found". Threads handle concurrent requests instead.
web: gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT app:app