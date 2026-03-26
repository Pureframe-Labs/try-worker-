FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy
WORKDIR /app

# Install system dependencies for OCR and PDF processing
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-mar \
    poppler-utils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The Microsoft Playwright image comes with browsers pre-installed,
# but we can ensure they are available just in case.
RUN playwright install chromium

# Copy worker application files
COPY . .

# Run the worker process
CMD ["python", "worker.py"]
