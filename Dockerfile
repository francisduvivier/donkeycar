FROM continuumio/miniconda3:4.5.11
# python 3.6

WORKDIR /app

# install donkey with tensorflow (cpu only version)
RUN conda update -n base -c defaults conda

RUN conda install mamba -n base -c conda-forge 
ADD . /app
WORKDIR /app
RUN mamba env create -f install/envs/ubuntu.yml
RUN echo 'conda activate donkey'>> /root/.bashrc
SHELL ["conda", "run", "-n", "donkey", "/bin/bash", "-c"]

RUN pip install -e .[pc]
RUN pip install -I --pre torch -f https://download.pytorch.org/whl/nightly/cu113/torch_nightly.html
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

# add the whole app dir after install so the pip install isn't updated when code changes.

#start the jupyter notebook

RUN echo "jupyter notebook --no-browser --ip 0.0.0.0 --port 8887 --allow-root  --notebook-dir=/app/notebooks" > /app/start.sh
RUN chmod +x /app/start.sh
ENTRYPOINT /app/start.sh

#port for donkeycar
EXPOSE 8887

#port for jupyter notebook
EXPOSE 8888

