FROM python:3.13-slim
WORKDIR /app
COPY server.py .
EXPOSE 8765
CMD ["python3", "-u", "server.py"]
