FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
# Single worker because reports are kept in memory (see Known Limitations)
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "app:app"]
