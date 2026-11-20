FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py storage.py payments.py post_lookup.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
