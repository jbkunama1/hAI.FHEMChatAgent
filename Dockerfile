FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m agent && mkdir /data && chown agent:agent /data
COPY *.py index.html ./
USER 10001:10001
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8080","--workers","1","--no-access-log"]
