FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py report_config.json ./

# Cloud Run Job runs this; override metrics/dates via report_config.json or args.
ENTRYPOINT ["python", "run_report.py"]
