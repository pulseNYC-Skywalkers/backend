FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8050

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the DuckDB at image build time from the bundled mock EHR API
# (fetch -> extract -> resolve -> decide), so the image is self-contained.
RUN sh scripts/build_db.sh

EXPOSE 8050
CMD ["sh", "-c", "uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT}"]
