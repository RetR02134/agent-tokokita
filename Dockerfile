FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py instructions.md startup.sh ./
COPY static ./static
RUN useradd -r -u 1001 appuser && chmod +x startup.sh
USER appuser
ENV PORT=8000 HOST=0.0.0.0
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz')" || exit 1
CMD ["./startup.sh"]
