import cv2
import torch
from ultralytics import YOLO
import datetime
import sqlite3
import numpy as np
from collections import defaultdict

class SceneAnalyzer:
    def __init__(self):
        # Initialize YOLO model
        self.model = YOLO('yolov8n.pt')
        self.conf_threshold = 0.4  # Increased for better accuracy
        
        # Initialize database
        self.init_database()
        
        # Track scene events with timestamps
        self.scene_events = defaultdict(list)
        self.start_time = None
        self.end_time = None
        
        # Track continuous presence of objects
        self.current_objects = defaultdict(int)
        self.object_timestamps = defaultdict(list)
        
    def init_database(self):
        self.conn = sqlite3.connect('scene_data.db')
        cursor = self.conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scene_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                event_type TEXT,
                objects TEXT,
                frame_number INTEGER,
                duration REAL
            )
        ''')
        self.conn.commit()

    def analyze_frame(self, frame, frame_number):
        # Get current timestamp
        current_time = datetime.datetime.now()
        if self.start_time is None:
            self.start_time = current_time
        self.end_time = current_time
        
        # Run YOLO detection
        results = self.model(frame, conf=self.conf_threshold)
        
        # Track objects in current frame
        current_frame_objects = defaultdict(int)
        detections = []
        
        for result in results:
            boxes = result.boxes
            for box in boxes:
                class_id = int(box.cls)
                class_name = self.model.names[class_id]
                confidence = float(box.conf)
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                
                # Count objects in current frame
                current_frame_objects[class_name] += 1
                
                # Store detection for visualization
                detections.append((class_name, confidence, (int(x1), int(y1), int(x2), int(y2))))
        
        # Analyze scene changes
        self.analyze_scene_change(current_frame_objects, current_time, frame_number)
        
        return detections

    def analyze_scene_change(self, current_frame_objects, timestamp, frame_number):
        # Check for new objects that appeared
        for obj, count in current_frame_objects.items():
            if obj not in self.current_objects:
                # New object appeared
                event = f"New {obj} appeared"
                self.store_event(event, timestamp, frame_number, [obj])
                self.object_timestamps[obj].append((timestamp, "appeared"))
            
            # Update continuous presence
            self.current_objects[obj] = count
        
        # Check for objects that disappeared
        for obj in list(self.current_objects.keys()):
            if obj not in current_frame_objects:
                # Object disappeared
                event = f"{obj} disappeared"
                self.store_event(event, timestamp, frame_number, [obj])
                self.object_timestamps[obj].append((timestamp, "disappeared"))
                del self.current_objects[obj]

    def store_event(self, event_type, timestamp, frame_number, objects):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO scene_events (timestamp, event_type, objects, frame_number, duration)
            VALUES (?, ?, ?, ?, ?)
        ''', (timestamp, event_type, ','.join(objects), frame_number, 0))
        self.conn.commit()

    def get_object_timeline(self, object_name):
        """Get the timeline of when an object appeared in the video"""
        if object_name.lower() in self.object_timestamps:
            events = self.object_timestamps[object_name.lower()]
            timeline = []
            for timestamp, event in events:
                seconds = (timestamp - self.start_time).total_seconds()
                minutes = int(seconds // 60)
                remaining_seconds = int(seconds % 60)
                timeline.append(f"{minutes}:{remaining_seconds:02d} - {event}")
            return timeline
        return ["Object not found in the video"]

    def generate_video_summary(self):
        summary = []
        
        # Calculate video duration
        duration = (self.end_time - self.start_time).total_seconds()
        summary.append(f"Video Duration: {duration:.1f} seconds")
        
        # Summarize key events
        summary.append("\nKey Events:")
        for obj, timestamps in self.object_timestamps.items():
            appearances = [t for t, e in timestamps if e == "appeared"]
            if appearances:
                first_seen = appearances[0]
                seconds = (first_seen - self.start_time).total_seconds()
                minutes = int(seconds // 60)
                remaining_seconds = int(seconds % 60)
                summary.append(f"- {obj.capitalize()} first appeared at {minutes}:{remaining_seconds:02d}")
        
        return "\n".join(summary)

    def start_camera(self):
        cap = cv2.VideoCapture(0)
        frame_number = 0
        
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                
                frame_number += 1
                
                # Analyze frame
                detections = self.analyze_frame(frame, frame_number)
                
                # Draw detection results
                for detection in detections:
                    object_name, confidence, bbox = detection
                    x1, y1, x2, y2 = bbox
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    label = f"{object_name}: {confidence:.2f}"
                    cv2.putText(frame, label, 
                              (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 
                              0.5, (0, 255, 0), 2)
                
                # Show frame number and current objects
                cv2.putText(frame, f"Frame: {frame_number}", 
                          (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 
                          1, (255, 255, 255), 2)
                
                # Display current objects
                y_pos = 70
                for obj, count in self.current_objects.items():
                    cv2.putText(frame, f"{obj}: {count}", 
                              (10, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 
                              0.6, (255, 255, 255), 2)
                    y_pos += 30
                
                # Display frame
                cv2.imshow('Scene Analysis', frame)
                
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
        finally:
            cap.release()
            cv2.destroyAllWindows()
            
            # Print video summary
            print("\n=== Video Analysis Summary ===")
            print(self.generate_video_summary())
            
            # Interactive query mode
            while True:
                query = input("\nAsk about any object in the video (or 'exit' to quit): ")
                if query.lower() == 'exit':
                    break
                
                timeline = self.get_object_timeline(query)
                print(f"\nTimeline for '{query}':")
                for event in timeline:
                    print(event)
            
            self.conn.close()

if __name__ == "__main__":
    analyzer = SceneAnalyzer()
    analyzer.start_camera()
