FROM python:3.14-alpine

WORKDIR /app

RUN pip install --no-cache-dir pyserial

COPY tcp_ecu/ ./tcp_ecu/

EXPOSE 30000

ENTRYPOINT ["python", "-m", "tcp_ecu.tcp.server"]
