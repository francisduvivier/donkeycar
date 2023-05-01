# Use the Miniconda 3 base image as a starting point
FROM tensorflow/tensorflow:2.9.3-gpu-jupyter

# Set the working directory to /app
WORKDIR /app

# Add the files necessary to set up the conda environment
ADD ./install /app/install

# Add setup files and README
ADD ./setup* /app/
ADD ./README.md /app/

# Install required packages with pip
RUN pip install --prefer-binary -e .[pc]

# Install testing requirements
RUN pip install -e .[dev]

# Configure Jupyter Notebook to run without password
RUN jupyter notebook --generate-config
RUN echo "c.NotebookApp.password = ''">>/root/.jupyter/jupyter_notebook_config.py
RUN echo "c.NotebookApp.token = ''">>/root/.jupyter/jupyter_notebook_config.py

# Expose ports for Donkey car and Jupyter Notebook
EXPOSE 8887
EXPOSE 8888

# To build, run the following command: docker build . -t donkey-cuda-jupyter
# To run, use docker-compose up -d 

