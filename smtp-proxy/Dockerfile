FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY smtp_proxy.py .
CMD ["python", "smtp_proxy.py"]
