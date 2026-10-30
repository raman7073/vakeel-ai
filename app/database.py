from pymongo import MongoClient
from config import MONGO_URI

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client.get_database()

# Collections
contracts_collection = db["contracts"]
analysis_collection = db["analysis"]
users_collection = db["users"]


def init_db():
    users_collection.create_index("username", unique=True)
    contracts_collection.create_index("owner_id")
    # Contracts use MongoDB's unique _id; they have no contract_id field.
    # Remove the legacy index from databases initialized by older versions.
    if "contract_id_1" in contracts_collection.index_information():
        contracts_collection.drop_index("contract_id_1")
    analysis_collection.create_index("analysis_id", unique=True)
    analysis_collection.create_index("contract_id")
