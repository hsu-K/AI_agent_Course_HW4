# 🛠️ Prerequisites
Before you begin, ensure you have the following installed:

* Python 3.11 (Strict requirement) 

* Docker Desktop (Required to run the Neo4j database)

* Google Cloud API Key (for Gemini-2.5-Flash-lite)
# ⚙️ Environment Setup
### 1. Database Setup (Neo4j via Docker)

You must run a local Neo4j instance using Docker. Run the following command in your terminal:

` docker run -d --name neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:latest `

Explanation of flags:

* -d: Runs the container in detached mode (background).

*  -p 7474:7474: Exposes the web interface port (Browser).

*  -p 7687:7687: Exposes the Bolt protocol port (Python connection).

*  -e NEO4J_AUTH=...: Sets the username (neo4j) and password (password).

Verification: After running the command, check if the database is ready:

1. Open your browser and go to http://localhost:7474.

2. Login with user: neo4j and password: password.

### 2. Virtual Environment Setup

It is highly recommended to use a virtual environment to manage dependencies.

**For macOS / Linux:**
```
# Create virtual environment
python -m venv venv

# Activate environment
source venv/bin/activate
```
**For Windows:**
```
# Create virtual environment
python -m venv venv

# Activate environment
venv\Scripts\activate
```

### 3. Install Dependencies

`pip install -r requirements.txt`

### 4. Environment Variables (.env)

Rename the file `.env_template` to `.env` in the root directory and add your GOOGLE_API_KEY

# 📂 File Descriptions

* **source/:** Folder containing raw English PDF regulations
* **setup_data.py:** Parses PDFs using pdfplumber and Regex, cleans the text, and stores structured data into a local SQLite database
* **build_kg.py:** Reads from SQLite and executes Cypher queries to create nodes (Regulation, Article) and relationships (HAS_ARTICLE) in Neo4j.
* **query_system.py:** The interactive chatbot. It retrieves full regulation context and uses the LLM to generate answers.
* **auto_test.py:** Runs 20 benchmark questions defined in test_data.json and uses an "LLM-as-a-Judge" to score your system (Pass/Fail).

# 🚀 Execution Order
**make sure you have already run neo4j in docker**
1. `python setup_data.py`
2. `python build_kg.py`
3. (Not necessary)`python query_system.py`: Test your system manually to see if it answers correctly.
4. `python auto_test.py`: run the benchmark test  



