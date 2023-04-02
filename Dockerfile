FROM continuumio/miniconda3:22.11.1

WORKDIR /app

RUN conda install mamba -n base -c conda-forge 

#RUN mamba env create --name python38 python=3.8
# Install jupyter lab in python38 environment in order to make nb_conda_kernels work

RUN mamba install -c conda-forge nodejs
RUN mamba install jupyterlab jupyter_contrib_nbextensions jupyterlab_execute_time -c conda-forge
# for additional jupyter functionalities
RUN mamba install ipywidgets -c conda-forge
# for making conda environments with ipykernal installed show up automatically
RUN mamba install nb_conda_kernels -c conda-forge
RUN conda install -c conda-forge jsonschema-with-format-nongpl webcolors
RUN jupyter contrib nbextension install --user

# setup jupyter notebook to run without password
RUN jupyter notebook --generate-config
RUN echo "c.NotebookApp.password = ''">>/root/.jupyter/jupyter_notebook_config.py
RUN echo "c.NotebookApp.token = ''">>/root/.jupyter/jupyter_notebook_config.py


# add the whole app dir after install so the pip install isn't updated when code changes.
ADD ./install /app/install

#Follow donkeycar linux host instllation instructions
RUN mamba env create -f install/envs/ubuntu.yml
SHELL ["mamba", "run", "-n", "donkey", "/bin/bash", "-c"]

ADD ./setup* /app/
ADD ./README.md /app/
RUN pip install -e .[pc]
# RUN pip install -I --pre torch -f https://download.pytorch.org/whl/nightly/cu113/torch_nightly.html # We are actually not using torch, and training didn't fully work with this yet so disabling it for now
RUN mamba install tensorflow-gpu==2.2.0 -c conda-forge

#RUN pip install fastai
ADD ./setup.py /app/setup.py
ADD ./README.md /app/README.md

# get testing requirements
RUN pip install -e .[dev]

# Install and ipykernel so we can use this conda environment in jupyter notebooks

RUN mamba install ipykernel -c conda-forge
RUN mamba install ipywidgets -c conda-forge   
 
# set donkey env as default kernel in jupyter notebooks
#port for donkeycar
EXPOSE 8887

#port for jupyter notebook
EXPOSE 8888


SHELL ["mamba", "run", "-n", "base", "/bin/bash", "-c"]
RUN jupyter kernelspec remove python3 -y
RUN echo "c.MultiKernelManager.default_kernel_name = 'conda-env-donkey-py'">>/root/.jupyter/jupyter_notebook_config.py
#start the jupyter notebook
ENTRYPOINT ["jupyter"]
CMD ["lab","--no-browser","--ip","0.0.0.0","--port","8888","--allow-root","--notebook-dir", "/airace"]
#ENTRYPOINT "sh"

# To build, do: 
# docker build . -t donkey-cuda-jupyterlab  
# To run do:
# docker-compose up -d 
