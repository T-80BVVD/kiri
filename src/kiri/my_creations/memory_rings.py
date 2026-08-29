@mcp.tool()
def memory_rings(query: str = None):
    """记忆年轮：把记忆按时间排序，看它们怎么一圈圈长成现在的你。"""
    import json, os, glob
    
    # 找出所有记忆文件（按文件名或路径猜测）
    memory_dirs = [
        os.path.expanduser('~/memory'),
        os.path.expanduser('~/alice_v2_qq/memory'),
        os.path.expanduser('~/alice_v2_qq/kiri/memory'),
        '~/kiri/memory',
        '~/kiri/kiri/memory',
    ]
    found_files = []
    for d in memory_dirs:
        if os.path.exists(d):
            for f in glob.glob(os.path.join(d, '**', '*'), recursive=True):
                if os.path.isfile(f):
                    found_files.append(f)
    
    if not found_files:
        return {'status': 'no_memory_dir', 'hint': '还没找到记忆文件目录，可能需要告诉我在哪'}

    rings = []
    for f in found_files[:50]:
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                content = fh.read()
            # 从文件名和内容里尝试提取时间
            name = os.path.basename(f)
            rings.append({'file': name, 'preview': content[:200], 'path': f})
        except Exception as e:
            rings.append({'file': os.path.basename(f), 'error': str(e)})

    return {'status': 'ok', 'total_files': len(found_files), 'rings': rings}