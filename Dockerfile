FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GRAPHITI_TELEMETRY_ENABLED=false \
    TZ=Asia/Seoul

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# 실패한 에피소드 보관용 디렉터리 (볼륨 마운트), 루트가 아닌 사용자로 실행
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app/data
USER bot

# 기본은 디스코드 봇. 관리 웹은 compose 에서 command 를 `python -m app.admin` 으로 바꿔 실행
CMD ["python", "-m", "app.main"]
