FROM continuumio/miniconda3:4.5.11
# python 3.6

WORKDIR /app

# install donkey with tensorflow (cpu only version)
RUN conda update -n base -c defaults conda

RUN conda install mamba -n base -c conda-forge 

# add the whole app dir after install so the pip install isn't updated when code changes.
ADD ./install /app/install
WORKDIR /app

#Follow donkeycar linux host instllation instructions
RUN mamba env create -f install/envs/ubuntu.yml
SHELL ["conda", "run", "-n", "donkey", "/bin/bash", "-c"]

ADD . /app
RUN pip install -e .[pc]
# RUN pip install -I --pre torch -f https://download.pytorch.org/whl/nightly/cu113/torch_nightly.html # We are actually not using torch, and training didn't fully work with this yet so disabling it for now
RUN conda install tensorflow-gpu==2.2.0

#RUN pip install fastai
ADD ./setup.py /app/setup.py
ADD ./README.md /app/README.md

# get testing requirements
RUN pip install -e .[dev]

# Install and ipykernel so we can use this conda environment in jupyter notebooks
RUN pip install ipykernel
RUN pip install ipywidgets   # for additional jupyter functionalities
 
RUN conda create --name python38 python=3.8
# Install jupyter lab in python38 environment in order to make nb_conda_kernels work
SHELL ["conda", "run", "-n", "python38", "/bin/bash", "-c"]
RUN conda install -c conda-forge jupyterlab
RUN conda install -c conda-forge ipywidgets   # for additional jupyter functionalities
RUN conda install -c conda-forge nb_conda_kernels

# setup jupyter notebook to run without password
RUN jupyter notebook --generate-config
RUN echo "c.NotebookApp.password = ''">>/root/.jupyter/jupyter_notebook_config.py
RUN echo "c.NotebookApp.token = ''">>/root/.jupyter/jupyter_notebook_config.py


#port for donkeycar
EXPOSE 8887

#port for jupyter notebook
EXPOSE 8888


#start the jupyter notebook
RUN echo "conda run -n python38 jupyter lab --no-browser --ip 0.0.0.0 --port 8888 --allow-root  --notebook-dir=/app/airace/notebooks" > /app/start.sh
RUN chmod +x /app/start.sh
ENTRYPOINT /app/start.sh

# To build, do: 
# docker build . -t donkey-cuda-jupyterlab  
# To run do:
# docker-compose up -d 

