# Use the Miniconda 3 base image as a starting point
FROM tensorflow/tensorflow:2.9.3-gpu-jupyter

RUN curl https://repo.anaconda.com/miniconda/Miniconda3-py38_23.3.1-0-Linux-x86_64.sh -o /tmp/miniconda_installer.sh
RUN chmod +x /tmp/miniconda_installer.sh
RUN /tmp/miniconda_installer.sh -b
# Set the working directory to /app
WORKDIR /app
ENV PATH=/root/miniconda3/bin:$PATH
# Install Mamba package manager for faster dependency resolution
RUN conda install mamba -n base -c conda-forge

# Add the files necessary to set up the conda environment
ADD ./install /app/install

# Create a Conda environment for the Donkey project
RUN mamba env create -f install/envs/ubuntu.yml

# Use Donkey Conda environment
SHELL ["mamba", "run", "-n", "donkey", "/bin/bash", "-c"]

# Add setup files and README
ADD ./setup* /app/
ADD ./README.md /app/

# Install required packages with pip
RUN pip install --prefer-binary -e .[pc]

# Install testing requirements
RUN pip install -e .[dev]

# Install JupyterLab and related extensions
RUN mamba install jupyterlab jupyter_contrib_nbextensions ipywidgets nb_conda_kernels nodejs jupyterlab_execute_time -c conda-forge

# Configure Jupyter Notebook to run without password
RUN jupyter notebook --generate-config
RUN echo "c.NotebookApp.password = ''">>/root/.jupyter/jupyter_notebook_config.py
RUN echo "c.NotebookApp.token = ''">>/root/.jupyter/jupyter_notebook_config.py

RUN pip install --upgrade nbformat==5.2

# Expose ports for Donkey car and Jupyter Notebook
EXPOSE 8887
EXPOSE 8888

# Set the entrypoint to start JupyterLab with the activated donkey environment
ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "donkey", "jupyter", "lab"]

# Set the default command to start JupyterLab with --allow-root
CMD ["--ip=0.0.0.0", "--port=8888", "--no-browser", "--notebook-dir=/airace", "--allow-root"]
# Instructions to build the Docker image and run the container
# To build, run the following command: docker build . -t donkey-cuda-jupyterlab
# To run, use docker-compose up -d 
