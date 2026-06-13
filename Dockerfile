FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY horizon_institutional_live_production_grade.py .
COPY docs/assets/architecture.svg docs/assets/architecture.svg
COPY scripts/performance_report.py scripts/performance_report.py

EXPOSE 8501

CMD ["python", "horizon_institutional_live_production_grade.py"]
