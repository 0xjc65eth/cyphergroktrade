FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code (config.py is gitignored, use template for cloud)
COPY *.py .
RUN if [ ! -f config.py ]; then cp config.template.py config.py; fi

# Run bot
CMD ["python", "bot.py"]
