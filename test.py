import os
import requests
import json

# ================= 配置区 =================
BASE_URL = os.getenv("LLM_BASE_URL", "http://192.168.1.4:3004/v1") 
API_KEY = os.getenv("LLM_API_KEY", "sk-nuz2utdjbgpXgYtV3NNzCfAJuYbNmqkviHQnk1QyJALq6k0H")
MODEL = os.getenv("LLM_MODEL", "gpt-5.2")
# ==========================================

# 1x1 红色像素的标准化 Base64 PNG
BASE64_IMAGE = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

def send_stream_request(test_name, payload, timeout=20):
    endpoint = BASE_URL.rstrip('/') + "/chat/completions"
    print(f"\n[{test_name}] 正在向 {endpoint} 发送流式请求 (stream=true)...")
    payload["stream"] = True
    
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout, stream=True)
        print(f"[{test_name} 返回状态码]: {response.status_code}")
        
        if response.status_code != 200:
            print(f"[{test_name} 报错信息]: {response.text}")
            return False
            
        print(f"[{test_name} 接收到的流式回复]: ", end="", flush=True)
        
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8').strip()
                if decoded_line.startswith("data: "):
                    data_str = decoded_line[6:]
                    
                    if data_str == "[DONE]":
                        break
                        
                    try:
                        data_json = json.loads(data_str)
                        # 增加物理边界检查，防止 choices 为空数组
                        choices = data_json.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                print(content, end="", flush=True)
                    except json.JSONDecodeError:
                        pass
                        
        print("\n")
        return True
            
    except Exception as e:
        print(f"\n[{test_name} 请求异常]: {str(e)}")
        return False

def run_test():
    print(f"[*] 测试目标模型: {MODEL}")
    
    # --- 测试 1：纯文本正常请求 ---
    text_payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "回复我数字 1 即可。"}],
        "max_tokens": 10
    }
    
    text_success = send_stream_request("测试 1: 纯文本", text_payload, timeout=10)
    
    if not text_success:
        print("[!] 纯文本请求失败，已终止后续多模态测试。")
        return

    # --- 测试 2：多模态图片请求 ---
    vision_payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这张图片是什么颜色的？请只回答颜色名称。"},
                    {
                        "type": "image_url", 
                        "image_url": {"url": f"data:image/png;base64,{BASE64_IMAGE}"}
                    }
                ]
            }
        ],
        "max_tokens": 50
    }
    
    send_stream_request("测试 2: 多模态", vision_payload, timeout=20)

if __name__ == "__main__":
    run_test()