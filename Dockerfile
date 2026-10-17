FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN groupadd --gid 10001 mixer && useradd --uid 10001 --gid mixer --create-home mixer
COPY gemini_web2api/ ./gemini_web2api/
COPY config.example.json ./config.json
EXPOSE 8081
USER 10001:10001
ENV MIXER_HOST=0.0.0.0
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('MIXER_PORT','8081')+'/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "gemini_web2api"]
