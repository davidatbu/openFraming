## Python version
This was made on Python 3.7.3. It should work on any Python3.

## Quick setup to get the server going

Clone this repo and `cd` into this directory.

First, setup a virtual environment and install the required packages.

	python -m venv /your/desired/directory/openFraming
	pip install -r requirements.txt
	source /your/desired/directory/openFraming/bin/activate

You should be able to run the server using.
	
	flask run --host=0.0.0.0 --port=5000 --debugger --reload 

If you go to your browser and try the following URLS, you should get a simple JSON 
response back.

 1. http://localhost:5000/classifiers/0/progress
 2. http://localhost:5000/classifiers/1/progress
 3. http://localhost:5000/classifiers/2/progress
