import cv2
import torch
import numpy as np
from ultralytics import YOLO
from transformers import pipeline
import sqlite3
from collections import defaultdict
import datetime
from typing import List, Dict, Tuple
import time
from PIL import Image
import os

class SceneUnderstanding:
    def __init__(self):
        # Initialize models
        self.yolo_model = YOLO('yolov8n.pt')
        
        # Initialize scene understanding pipeline
        self.scene_pipeline = pipeline("image-to-text", model="Salesforce/blip-image-captioning-large")
        
        # Initialize action recognition pipeline
        self.action_pipeline = pipeline("zero-shot-image-classification", model="openai/clip-vit-base-patch32")
        self.action_labels = [
            "sitting", "standing", "walking", "running",
            "eating", "drinking", "reading", "writing",
            "typing", "talking", "listening", "thinking"
        ]
        
        # Initialize trackers
        self.object_tracker = defaultdict(list)
        self.action_memory = []
        self.scene_memory = []
        
        # Database setup
        self.init_database()
        
        # Configuration
        self.conf_threshold = 0.4
        
    def init_database(self):
        """Initialize SQLite database for storing scene understanding results"""
        self.conn = sqlite3.connect('scene_understanding.db')
        cursor = self.conn.cursor()
        
        # Create tables for storing different types of understanding
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scene_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                event_type TEXT,
                description TEXT,
                confidence REAL,
                objects_involved TEXT,
                action_detected TEXT
            )
        ''')
        self.conn.commit()
        
    def analyze_actions(self, frame: np.ndarray) -> Tuple[str, float]:
        """Analyze frame to detect actions"""
        # Convert frame to RGB
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb_frame)
        
        # Get action predictions
        predictions = self.action_pipeline(pil_image, candidate_labels=self.action_labels)
        
        # Get top action and confidence
        top_action = predictions[0]
        return top_action['label'], top_action['score']
        
    def understand_scene(self, frame: np.ndarray) -> Dict:
        """Generate comprehensive scene understanding"""
        # Convert frame to RGB for captioning
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb_frame)
        
        # Get scene caption
        caption = self.scene_pipeline(pil_image)[0]['generated_text']
        
        # Detect objects
        results = self.yolo_model(frame, conf=self.conf_threshold)
        
        # Extract detected objects
        detected_objects = []
        for r in results:
            for box in r.boxes:
                obj = {
                    'class': r.names[int(box.cls)],
                    'confidence': float(box.conf),
                    'bbox': box.xyxy[0].tolist()
                }
                detected_objects.append(obj)
        
        # Analyze actions
        action, action_conf = self.analyze_actions(frame)
        
        # Compile comprehensive understanding
        understanding = {
            'timestamp': datetime.datetime.now(),
            'scene_description': caption,
            'detected_objects': detected_objects,
            'action_detected': action,
            'action_confidence': action_conf
        }
        
        # Store in database
        self.store_understanding(understanding)
        
        return understanding
        
    def store_understanding(self, understanding: Dict):
        """Store scene understanding in database"""
        cursor = self.conn.cursor()
        
        # Prepare data for storage
        objects_str = ', '.join([obj['class'] for obj in understanding['detected_objects']])
        
        cursor.execute('''
            INSERT INTO scene_events 
            (timestamp, event_type, description, confidence, objects_involved, action_detected)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            understanding['timestamp'],
            'scene_understanding',
            understanding['scene_description'],
            understanding.get('action_confidence', 0.0),
            objects_str,
            understanding['action_detected']
        ))
        
        self.conn.commit()
        
    def start_camera(self):
        """Start camera feed and begin scene understanding"""
        print("Starting camera feed...")
        
        try:
            print("Checking available cameras...")
            # Try different camera indices
            camera_found = False
            for idx in range(4):  # Try indices 0-3
                try:
                    print(f"\nTrying camera index {idx}")
                    cap = cv2.VideoCapture(idx)
                    if cap.isOpened():
                        print(f"Successfully opened camera {idx}")
                        camera_found = True
                        break
                    else:
                        print(f"Could not open camera {idx}")
                        cap.release()
                except Exception as e:
                    print(f"Error trying camera {idx}: {str(e)}")
            
            if not camera_found:
                print("\nNo cameras found. Please check:")
                print("1. Camera is properly connected")
                print("2. Camera permissions (try: ls -l /dev/video*)")
                print("3. Camera drivers are loaded (try: lsmod | grep uvcvideo)")
                return
                
            print("\nCamera opened successfully. Press 'q' to quit.")
            
            # Create window with different flags
            window_name = 'Scene Understanding'
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
            
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("Error: Could not read frame")
                    break
                
                # Get frame dimensions
                height, width = frame.shape[:2]
                print(f"Frame size: {width}x{height}")
                
                # Resize frame if needed
                if width > 800:
                    frame = cv2.resize(frame, (800, int(800*height/width)))
                
                # Display frame
                cv2.imshow(window_name, frame)
                
                try:
                    # Process frame
                    understanding = self.understand_scene(frame)
                    
                    # Draw scene description
                    cv2.putText(frame, f"Scene: {understanding['scene_description'][:50]}...",
                               (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    
                    # Draw action
                    cv2.putText(frame, f"Action: {understanding['action_detected']} ({understanding['action_confidence']:.2f})",
                               (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    
                    # Draw detected objects
                    for obj in understanding['detected_objects']:
                        bbox = [int(x) for x in obj['bbox']]
                        cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (0, 255, 0), 2)
                        cv2.putText(frame, f"{obj['class']} ({obj['confidence']:.2f})",
                                   (bbox[0], bbox[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                    
                    # Show processed frame
                    cv2.imshow(window_name, frame)
                except Exception as e:
                    print(f"Frame processing error: {e}")
                    cv2.imshow(window_name, frame)
                
                # Break on 'q' press
                if cv2.waitKey(30) & 0xFF == ord('q'):
                    print("Q pressed, exiting...")
                    break
                    
        except Exception as e:
            print(f"Camera feed error: {str(e)}")
        finally:
            print("\nCleaning up...")
            if 'cap' in locals():
                cap.release()
            cv2.destroyAllWindows()
            cv2.waitKey(1)
            self.conn.close()
            
    def query_past_events(self, minutes: int = 5) -> List[Dict]:
        """Query past events from database"""
        cursor = self.conn.cursor()
        
        # Get events from last n minutes
        past_time = datetime.datetime.now() - datetime.timedelta(minutes=minutes)
        
        cursor.execute('''
            SELECT timestamp, description, action_detected, objects_involved
            FROM scene_events
            WHERE timestamp > ?
            ORDER BY timestamp DESC
        ''', (past_time,))
        
        events = []
        for row in cursor.fetchall():
            events.append({
                'timestamp': row[0],
                'description': row[1],
                'action': row[2],
                'objects': row[3]
            })
            
        return events

if __name__ == "__main__":
    scene_analyzer = SceneUnderstanding()
    scene_analyzer.start_camera()
