docker build . -t donkey-cuda-jupyter &&\
docker-compose up -d &&\
docker logs race2thefuture__donkeycar_donkey-container_1 -f
