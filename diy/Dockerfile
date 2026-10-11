# Sandbox image for the DIY agent. Contains only what the analysis needs.
FROM python:3.12-slim

RUN pip install --no-cache-dir pandas numpy matplotlib

# Non-root user. The uid matches the host user so files written to the
# bind-mounted workspace are readable afterwards.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin agent
USER agent
WORKDIR /workspace
