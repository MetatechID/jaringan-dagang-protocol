FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 4030

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "4030"]
