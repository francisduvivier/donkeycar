FROM python:3.9

WORKDIR /app

# install donkey with tensorflow (cpu only version)
RUN wget https://repo.anaconda.com/miniconda/Miniconda3-py39_23.1.0-1-Linux-x86_64.sh
RUN chmod +x Miniconda3-py39_23.1.0-1-Linux-x86_64.sh && bash ./Miniconda3-py39_23.1.0-1-Linux-x86_64.sh  -b -p /root/miniconda
RUN /root/miniconda/bin/conda install -y -c conda-forge cudatoolkit=11.6.* cudnn=8.1.0
RUN /root/miniconda/bin/conda init bash
#SHELL ["/bin/bash", "--rcfile","/root/.bashrc","-c"]
SHELL ["/root/miniconda/bin/conda", "run", "/bin/bash", "-c"]
RUN conda install -y pytorch pytorch-cuda=11.6 -c pytorch -c nvidia
RUN echo 'export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CONDA_PREFIX/lib/'>> /root/.bashrc

RUN /root/miniconda/bin/conda install -y tensorflow==2.9.* torchvision 
RUN pip install fastai
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
ADD . /app

#start the jupyter notebook
CMD jupyter notebook --no-browser --ip 0.0.0.0 --port 8888 --allow-root  --notebook-dir=/app/notebooks

#port for donkeycar
EXPOSE 8887

#port for jupyter notebook
EXPOSE 8888
