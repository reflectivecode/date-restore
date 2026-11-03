FROM python:3.15-alpine

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY date-restore.py .

RUN chmod 755 -R /app

ENTRYPOINT ["python3", "-u", "/app/date-restore.py"]
