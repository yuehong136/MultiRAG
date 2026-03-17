MultiRAG Chat Plugin for ChatGPT-on-WeChat
=========================================

This folder contains the source code for the `multirag_chat` plugin, which extends the core functionality of the MultiRAG API to support conversational interactions using Retrieval-Augmented Generation (RAG). This plugin integrates seamlessly with the [ChatGPT-on-WeChat](https://github.com/zhayujie/chatgpt-on-wechat) project, enabling WeChat and other platforms to leverage the knowledge retrieval capabilities provided by MultiRAG in chat interactions.

### Features
* **Conversational Interactions**: Combine WeChat's conversational interface with powerful RAG (Retrieval-Augmented Generation) capabilities.
* **Knowledge-Based Responses**: Enrich conversations by retrieving relevant data from external knowledge sources and incorporating them into chat responses.
* **Multi-Platform Support**: Works across WeChat, WeCom, and various other platforms supported by the ChatGPT-on-WeChat framework.

### Plugin vs. ChatGPT-on-WeChat Configurations
**Note**: There are two distinct configuration files used in this setup—one for the ChatGPT-on-WeChat core project and another specific to the `multirag_chat` plugin. It is important to configure both correctly to ensure smooth integration.

#### ChatGPT-on-WeChat Root Configuration (`config.json`)
This file is located in the root directory of the [ChatGPT-on-WeChat](https://github.com/zhayujie/chatgpt-on-wechat) project and is responsible for defining the communication channels and overall behavior. For example, it handles the configuration for WeChat, WeCom, and other services like Feishu and DingTalk.

Example `config.json` (for WeChat channel):
```json
{
  "channel_type": "wechatmp",
  "wechatmp_app_id": "YOUR_APP_ID",
  "wechatmp_app_secret": "YOUR_APP_SECRET",
  "wechatmp_token": "YOUR_TOKEN",
  "wechatmp_port": 80,
  ...
}
```

This file can also be modified to support other communication platforms, such as:
- **Personal WeChat** (`channel_type: wx`)
- **WeChat Public Account** (`wechatmp` or `wechatmp_service`)
- **WeChat Work (WeCom)** (`wechatcom_app`)
- **Feishu** (`feishu`)
- **DingTalk** (`dingtalk`)

For detailed configuration options, see the official [LinkAI documentation](https://docs.link-ai.tech/cow/multi-platform/wechat-mp).

#### MultiRAG Chat Plugin Configuration (`plugins/multirag_chat/config.json`)
This configuration is specific to the `multirag_chat` plugin and is used to set up communication with the MultiRAG server. Ensure that your MultiRAG server is running, and update the plugin's `config.json` file with your server details:

Example `config.json` (for `multirag_chat`):
```json
{
  "multirag_api_key": "YOUR_API_KEY",
  "multirag_host": "127.0.0.1:80"
}
```

This file must be configured to point to your MultiRAG instance, with the `multirag_api_key` and `multirag_host` fields set appropriately. The `multirag_host` is typically your server's address and port number, and the `multirag_api_key` is obtained from your MultiRAG API setup.

### Requirements
Before you can use this plugin, ensure the following are in place:

1. You have installed and configured [ChatGPT-on-WeChat](https://github.com/zhayujie/chatgpt-on-wechat).
2. You have deployed and are running the [MultiRAG](https://github.com/yuehong136/multirag) server.
   
Make sure both `config.json` files (ChatGPT-on-WeChat and MultiRAG Chat Plugin) are correctly set up as per the examples above.
