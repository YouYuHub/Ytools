import os
import shutil

def delete_pycache_directories(root_directory = None) -> None:
    """删除指定目录下的所有__pycache__目录，递归删除所有子目录"""
    # 空值默认删除当前目录下的
    if not root_directory:
        root_directory = os.getcwd()
    # 检查root_directory是否存在
    if not os.path.exists(root_directory):
        print(f"{root_directory} does not exist")
    if not os.path.isdir(root_directory):
        print(f"{root_directory} is not a directory")
    for root, subdirectories, _ in os.walk(root_directory, topdown=False):
        for subdirectory in subdirectories:
            if subdirectory == '__pycache__':
                pycache_path = os.path.join(root, subdirectory)
                try:
                    shutil.rmtree(pycache_path)
                    print(f"Deleted: {pycache_path}")
                except Exception as e:
                    print(f"Failed to delete {pycache_path}: {e}")

# Usage
# root_path = input("input the delete __pycache__ dir: ")
# delete_pycache_directories(root_path.strip(r'"'))
delete_pycache_directories()
delete_pycache_directories(None)
