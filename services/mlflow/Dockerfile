FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir mlflow==2.22.0

EXPOSE 5000

CMD ["mlflow", "server", "--host", "0.0.0.0", "--port", "5000", "--backend-store-uri", "sqlite:////data/mlflow.db", "--default-artifact-root", "/data/artifacts"]
