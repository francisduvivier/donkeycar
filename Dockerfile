FROM continuumio/miniconda3:4.12.0 
# python 3.9

WORKDIR /app

# install donkey with tensorflow (cpu only version)
RUN conda init bash
RUN conda create --name donkey 
RUN echo 'conda activate donkey'>> /root/.bashrc
SHELL ["conda", "run", "-n", "donkey", "/bin/bash", "-c"]
RUN conda install -y -c conda-forge cudatoolkit=11.2.* cudnn=8.1.0
RUN echo 'export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CONDA_PREFIX/lib/'>> /root/.bashrc
RUN python3 -m pip install tensorflow==2.9.*

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
ADD . /app

#start the jupyter notebook

RUN echo "jupyter notebook --no-browser --ip 0.0.0.0 --port 8888 --allow-root  --notebook-dir=/app/notebooks" > start.sh
RUN chmod +x start.sh
ENTRYPOINT /bin/bash

#port for donkeycar
EXPOSE 8887

#port for jupyter notebook
EXPOSE 8888
