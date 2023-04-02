FROM continuumio/miniconda3:4.5.11
# python 3.6

WORKDIR /app

# install donkey with tensorflow (cpu only version)
RUN conda update -n base -c defaults conda

RUN conda install mamba -n base -c conda-forge 

# add the whole app dir after install so the pip install isn't updated when code changes.
ADD . /app
WORKDIR /app

#Follow donkeycar linux host instllation instructions
RUN mamba env create -f install/envs/ubuntu.yml
RUN echo 'conda activate donkey'>> /root/.bashrc
SHELL ["conda", "run", "-n", "donkey", "/bin/bash", "-c"]

RUN pip install -e .[pc]
# RUN pip install -I --pre torch -f https://download.pytorch.org/whl/nightly/cu113/torch_nightly.html # We are actually not using torch, and training didn't fully work with this yet so disabling it for now
RUN conda install tensorflow-gpu==2.2.0

#RUN pip install fastai
ADD ./setup.py /app/setup.py
ADD ./README.md /app/README.md

# get testing requirements
RUN pip install -e .[dev]

# setup jupyter notebook to run without password
RUN pip install jupyter notebook
RUN jupyter notebook --generate-config
RUN echo "c.NotebookApp.password = ''">>/root/.jupyter/jupyter_notebook_config.py
RUN echo "c.NotebookApp.token = ''">>/root/.jupyter/jupyter_notebook_config.py


#port for donkeycar
EXPOSE 8887

#port for jupyter notebook
EXPOSE 8888

#start the jupyter notebook
RUN echo "jupyter notebook --no-browser --ip 0.0.0.0 --port 8888 --allow-root  --notebook-dir=/app/airace/notebooks" > /app/start.sh
RUN chmod +x /app/start.sh
ENTRYPOINT conda run -n donkey /app/start.sh

# Use this with: 
# docker run -d -p 8888:8888 -p 8887:8887 --gpus all --name donkey-container --shm-size=64 -v /home/local_sqs-ai/airace:/app/airace donkey-cuda
