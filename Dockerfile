FROM python:3.11-slim

WORKDIR /app
COPY app ./app
COPY tests ./tests
COPY verify ./verify

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "app.server"]
