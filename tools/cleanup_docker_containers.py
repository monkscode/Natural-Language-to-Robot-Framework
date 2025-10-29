#!/usr/bin/env python3
"""
Clean up Docker containers that might be causing conflicts.
"""

import docker
import logging

def cleanup_containers():
    """Clean up all test-related Docker containers."""
    try:
        client = docker.from_env()
        
        print("🧹 Cleaning up Docker containers...")
        
        # Get all containers (running and stopped)
        all_containers = client.containers.list(all=True)
        
        cleaned_count = 0
        for container in all_containers:
            container_name = container.name
            
            # Clean up test-related containers
            if any(prefix in container_name for prefix in ['robot-test-', 'test-runner-']):
                try:
                    print(f"   Removing container: {container_name}")
                    container.remove(force=True)
                    cleaned_count += 1
                except Exception as e:
                    print(f"   ⚠️  Failed to remove {container_name}: {e}")
        
        print(f"✅ Cleaned up {cleaned_count} containers")
        
        # Also clean up any dangling containers
        try:
            client.containers.prune()
            print("✅ Pruned dangling containers")
        except Exception as e:
            print(f"⚠️  Failed to prune containers: {e}")
        
        return True
        
    except docker.errors.DockerException as e:
        print(f"❌ Docker error: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False

def main():
    """Main cleanup function."""
    print("Docker Container Cleanup Tool")
    print("=" * 40)
    
    if cleanup_containers():
        print("\n🎉 Container cleanup completed successfully!")
        print("You can now restart your application.")
    else:
        print("\n❌ Container cleanup failed.")
        print("Please check Docker Desktop is running and try again.")

if __name__ == "__main__":
    main()