import sqlite3
from datetime import datetime

def query_scene_information(object_name=None):
    conn = sqlite3.connect('scene_data.db')
    cursor = conn.cursor()
    
    if object_name:
        # Query specific object
        cursor.execute('''
            SELECT timestamp, object_name, confidence 
            FROM detections 
            WHERE object_name LIKE ? 
            ORDER BY timestamp DESC
        ''', (f'%{object_name}%',))
    else:
        # Get all detections
        cursor.execute('''
            SELECT timestamp, object_name, confidence 
            FROM detections 
            ORDER BY timestamp DESC
        ''')
    
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        print(f"No detections found{' for ' + object_name if object_name else ''}")
        return
    
    print("\nScene Information:")
    print("-----------------")
    for timestamp, obj_name, confidence in results:
        print(f"Time: {timestamp}")
        print(f"Object: {obj_name}")
        print(f"Confidence: {confidence:.2f}")
        print("-----------------")

if __name__ == "__main__":
    while True:
        print("\nScene Query Interface")
        print("1. Search for specific object")
        print("2. Show all detections")
        print("3. Exit")
        
        choice = input("Enter your choice (1-3): ")
        
        if choice == "1":
            object_name = input("Enter object to search for: ")
            query_scene_information(object_name)
        elif choice == "2":
            query_scene_information()
        elif choice == "3":
            break
        else:
            print("Invalid choice. Please try again.")
