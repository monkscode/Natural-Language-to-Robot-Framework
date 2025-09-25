import asyncio
import logging
import os
import json
import time
import threading
import uuid
import sys
from flask import Flask, request, jsonify
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any

# Fix encoding issues on Windows
if sys.platform.startswith('win'):
    os.environ['PYTHONIOENCODING'] = 'utf-8'

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    encoding="utf-8"
)
logger = logging.getLogger(__name__)

from dotenv import load_dotenv
from browser_use import Agent, Browser
from browser_use.browser.session import BrowserSession

load_dotenv("src/backend/.env")

app = Flask(__name__)

# Task storage to keep track of tasks
tasks: Dict[str, Dict[str, Any]] = {}

# Initialize a thread pool executor for running tasks in the background
executor = ThreadPoolExecutor(max_workers=1)

# Google API configuration
GOOGLE_API_KEY = os.environ.get('GEMINI_API_KEY')

def process_task(task_id: str, objective: str) -> None:
    """Process the browser automation task in the background."""
    logger.info(f"Starting Browser-Use agent with objective: {objective}")
    
    # Update task status to running
    tasks[task_id]['status'] = 'running'
    tasks[task_id]['started_at'] = time.time()

    async def run_browser_use(objective: str) -> Dict[str, Any]:
        session = None
        try:
            # Initialize browser session with better settings
            session = BrowserSession(
                headless=False,  # Set to True for production, False for debugging
                viewport={'width': 1920, 'height': 1080},
                # Add additional browser arguments
            )
            
            # Import ChatGoogle here to avoid import issues
            try:
                from browser_use.llm.google import ChatGoogle
            except ImportError:
                logger.error("Failed to import ChatGoogle. Make sure browser-use is properly installed.")
                raise ImportError("ChatGoogle not available")
            
            # Enhanced objective with specific guidance for dynamic sites
            enhanced_objective = f"""
            {objective}
            
            IMPORTANT INSTRUCTIONS:
            1. When looking for CSS selectors, use more generic approaches like:
               - document.querySelector('[data-testid*="product"]')
               - document.querySelector('a[title]') for product names
               - document.querySelector('[class*="price"]') for prices
            
            2. If specific selectors fail, use these fallback strategies:
               - Extract data using textContent from visible elements
               - Use XPath selectors instead of CSS
               - Look for elements by their visible text content
            
            3. For e-commerce sites like Flipkart:
               - Product names are usually in <a> tags with title attributes
               - Prices often contain currency symbols (₹, $)
               - Use document.querySelectorAll to get all products, then focus on the first
            
            4. If CSS selector detection fails, simply extract and return the actual data instead
            
            5. Handle encoding properly for international characters and currency symbols
            """

            logger.info(f"Gemini Key used is: {GOOGLE_API_KEY}")
            # Initialize the agent with enhanced settings
            agent = Agent(
                task=enhanced_objective,
                browser_context=session,
                llm=ChatGoogle(
                    model="gemini-2.5-flash",
                    api_key=GOOGLE_API_KEY,
                    temperature=0.1  # Lower temperature for more consistent results
                ),
                use_vision=True,  # Enable vision for better automation
                max_steps=25,  # Reduce steps to prevent infinite loops
                # Add custom instructions for better performance
                system_prompt="""You are an expert web automation agent. When working with dynamic websites:
                1. Be flexible with CSS selectors - sites often use dynamic class names
                2. If exact selectors fail, extract the actual data instead of continuing to search for selectors
                3. Use multiple fallback strategies
                4. Handle encoding issues gracefully
                5. Focus on getting results rather than perfect selectors"""
            )
            
            logger.info("Agent initialized, starting execution...")
            results = await agent.run()
            
            # Enhanced result extraction with error handling
            final_result = ""
            success = False
            
            try:
                if hasattr(results, 'final_result'):
                    final_result = str(results.final_result())
                else:
                    final_result = str(results)
                
                # Consider it successful if we have meaningful data
                success = bool(final_result and len(final_result.strip()) > 10)
                
                logger.info(f"Agent execution completed. Success: {success}, Result: {final_result[:200]}...")
                
            except Exception as e:
                logger.error(f"Error extracting final result: {e}")
                final_result = f"Task completed but result extraction failed: {str(e)}"
                success = False
            
            return {
                'success': success,
                'result': final_result,
                'steps_taken': getattr(results, 'steps_taken', 0),
                'execution_time': time.time() - tasks[task_id]['started_at'],
                'agent_status': 'completed'
            }
            
        except Exception as e:
            error_msg = str(e)
            # Handle encoding errors specifically
            if 'charmap' in error_msg or 'codec' in error_msg:
                error_msg = f"Encoding error occurred. This is often due to special characters like currency symbols. Error: {error_msg}"
            
            logger.error(f"Error in browser automation: {error_msg}", exc_info=True)
            return {
                'success': False,
                'error': error_msg,
                'execution_time': time.time() - tasks[task_id].get('started_at', time.time()),
                'agent_status': 'failed'
            }
        finally:
            # Proper cleanup
            if session:
                try:
                    # Use the correct method for closing browser session
                    if hasattr(session, 'close'):
                        await session.close()
                    elif hasattr(session, 'browser') and hasattr(session.browser, 'close'):
                        await session.browser.close()
                    else:
                        # Fallback cleanup
                        logger.warning("Could not find proper close method for browser session")
                    
                    logger.info("Browser session closed successfully")
                except Exception as e:
                    logger.error(f"Error closing browser session: {e}")

    # Run the automation in a new event loop
    loop = None
    try:
        # Set UTF-8 encoding for the current thread
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(run_browser_use(objective))
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in event loop execution: {error_msg}", exc_info=True)
        results = {
            'success': False,
            'error': f"Event loop error: {error_msg}",
            'execution_time': time.time() - tasks[task_id].get('started_at', time.time()),
            'agent_status': 'failed'
        }
    finally:
        if loop:
            try:
                # Clean up any pending tasks
                pending = asyncio.all_tasks(loop)
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                
                loop.close()
            except Exception as e:
                logger.error(f"Error closing event loop: {e}")

    # Update the task status and results    
    tasks[task_id].update({
        "status": "completed",
        "completed_at": time.time(),
        "message": f"Objective completed: {objective[:100]}{'...' if len(objective) > 100 else ''}",
        "results": results
    })
    
    logger.info(f"Task {task_id} completed. Success: {results.get('success', False)}")

@app.route('/', methods=['GET'])
def root():
    """Root endpoint to verify service is running."""
    return jsonify({
        "service": "Enhanced Browser Use Service",
        "status": "running",
        "version": "2.0.0",
        "improvements": [
            "Better encoding handling",
            "Enhanced error handling", 
            "Improved CSS selector strategies",
            "Proper session cleanup",
            "Fallback mechanisms"
        ],
        "endpoints": [
            "GET / - This endpoint",
            "GET /health - Health check", 
            "GET /probe - Legacy health check",
            "POST /submit - Submit automation task",
            "GET /query/<task_id> - Query task status",
            "GET /tasks - List all tasks"
        ]
    }), 200

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "enhanced_browser_use_service",
        "timestamp": time.time(),
        "active_tasks": len([t for t in tasks.values() if t.get('status') in ['processing', 'running']]),
        "total_tasks": len(tasks),
        "encoding": "utf-8",
        "google_api_configured": bool(GOOGLE_API_KEY and GOOGLE_API_KEY != 'your_api_key_here')
    }), 200

@app.route('/probe', methods=['GET'])
def probe():
    """Legacy probe endpoint for backward compatibility."""
    return jsonify({"status": "alive", "message": "enhanced_browser_use_service is alive"}), 200

@app.route('/submit', methods=['POST'])
def submit():
    """Submit a new browser automation task."""
    try:
        # Check if request has JSON data
        if not request.is_json:
            return jsonify({
                "status": "error", 
                "message": "Request must be JSON with Content-Type: application/json"
            }), 400
            
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data provided."}), 400
            
        objective = data.get("browser_use_objective")
        if not objective:
            return jsonify({"status": "error", "message": "No 'browser_use_objective' field provided."}), 400

        # Check if service is busy (only one task at a time)
        active_tasks = [t for t in tasks.values() if t.get('status') in ['processing', 'running']]
        if active_tasks:
            return jsonify({
                "status": "busy", 
                "message": "Service is currently processing another task. Please try again later.",
                "active_tasks": len(active_tasks)
            }), 429

        # Generate a unique task ID
        task_id = str(uuid.uuid4())

        # Initialize the task with "processing" status
        tasks[task_id] = {
            "status": "processing",
            "objective": objective,
            "created_at": time.time(),
            "started_at": None,
            "completed_at": None
        }

        # Submit the task to the thread pool executor
        future = executor.submit(process_task, task_id, objective)
        tasks[task_id]['future'] = future

        logger.info(f"Task {task_id} submitted with objective: {objective[:100]}{'...' if len(objective) > 100 else ''}")

        # Return the task ID immediately
        return jsonify({
            "status": "processing", 
            "task_id": task_id,
            "message": "Task submitted successfully with enhanced processing"
        }), 202
        
    except Exception as e:
        logger.error(f"Error in submit endpoint: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500

@app.route('/query/<task_id>', methods=['GET'])
def query(task_id: str):
    """Query the status of a specific task."""
    try:
        if task_id not in tasks:
            return jsonify({"status": "error", "message": "Task ID not found."}), 404

        task = tasks[task_id]
        
        # Prepare response data
        response_data = {
            "task_id": task_id,
            "status": task.get("status", "unknown"),
            "objective": task.get("objective", "")[:200],  # Truncate long objectives
            "created_at": task.get("created_at"),
        }
        
        status = task.get("status")
        
        if status == "processing":
            return jsonify(response_data), 202
        elif status == "running":
            response_data.update({
                "started_at": task.get("started_at"),
                "running_time": time.time() - task.get("started_at", time.time()) if task.get("started_at") else 0
            })
            return jsonify(response_data), 202
        elif status == "completed":
            response_data.update({
                "started_at": task.get("started_at"),
                "completed_at": task.get("completed_at"),
                "message": task.get("message"),
                "results": task.get("results", {}),
                "total_time": (task.get("completed_at", time.time()) - task.get("created_at", time.time())) if task.get("created_at") else 0
            })
            logger.info(f"Task {task_id} query completed: {task.get('results', {}).get('success', False)}")
            return jsonify(response_data), 200
        else:
            return jsonify(response_data), 200
            
    except Exception as e:
        logger.error(f"Error in query endpoint: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500

@app.route('/tasks', methods=['GET'])
def list_tasks():
    """List all tasks with their status."""
    try:
        task_list = []
        for task_id, task_data in tasks.items():
            task_summary = {
                "task_id": task_id,
                "status": task_data.get("status", "unknown"),
                "objective": task_data.get("objective", "")[:100],  # Truncate for display
                "created_at": task_data.get("created_at"),
            }
            if task_data.get("completed_at"):
                task_summary["completed_at"] = task_data["completed_at"]
                task_summary["success"] = task_data.get("results", {}).get("success", False)
            task_list.append(task_summary)
        
        return jsonify({
            "tasks": task_list,
            "total_tasks": len(task_list),
            "active_tasks": len([t for t in tasks.values() if t.get('status') in ['processing', 'running']])
        }), 200
        
    except Exception as e:
        logger.error(f"Error in list_tasks endpoint: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({
        "status": "error",
        "message": "Endpoint not found",
        "available_endpoints": [
            "GET / - Service info",
            "GET /health - Health check", 
            "GET /probe - Legacy health check",
            "POST /submit - Submit task",
            "GET /query/<task_id> - Query task",
            "GET /tasks - List tasks"
        ]
    }), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return jsonify({
        "status": "error",
        "message": "Internal server error",
        "error": str(error)
    }), 500

if __name__ == '__main__':
    # Set encoding environment variables
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    
    logger.info("Starting Enhanced Browser Use Service...")
    logger.info(f"Service will run on http://0.0.0.0:4999")
    logger.info("Available endpoints:")
    logger.info("  GET  / - Service information")
    logger.info("  GET  /health - Health check")
    logger.info("  GET  /probe - Legacy health check")
    logger.info("  POST /submit - Submit new task")
    logger.info("  GET  /query/<task_id> - Query task status")
    logger.info("  GET  /tasks - List all tasks")
    
    logger.info("Enhanced features:")
    logger.info("  - Better encoding handling for international characters")
    logger.info("  - Improved CSS selector detection strategies") 
    logger.info("  - Enhanced error handling and recovery")
    logger.info("  - Proper browser session cleanup")
    logger.info("  - Fallback mechanisms for dynamic sites")
    
    app.run(debug=False, host='0.0.0.0', port=4999, threaded=True)