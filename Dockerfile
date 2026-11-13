# Wavelog DX Checker - standard-library Python only, no pip installs.
FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    STATE_DIR=/data

WORKDIR /app
COPY dxcheck.py mailwatch.py settings.py app.py index.html setup.html login.html ./

# Run as an unprivileged user; settings live on /data, readable only by that user.
RUN adduser -D -H -u 10001 app && mkdir /data && chown app:app /data && chmod 700 /data
VOLUME /data
USER app

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8080'),timeout=4)" || exit 1

CMD ["python", "app.py"]
