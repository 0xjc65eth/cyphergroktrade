FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code (config.py is gitignored, use template for cloud)
COPY *.py .
COPY dashboard.html .
COPY render.yaml .
RUN if [ ! -f config.py ]; then cp config.template.py config.py; fi

# Expose health check port (Render uses PORT env var)
EXPOSE 10000

# Run via web wrapper (keeps Render free tier alive)
CMD ["python", "web_wrapper.py"]
