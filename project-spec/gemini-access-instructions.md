## Gemini access instructions

1. Install the Google CLI <u>https://cloud.google.com/sdk/docs/install</u>

2. Authenticate with `gcloud auth application-default login` command - use your griddynamics email and gd-gcpgridu-genai project.

If done right, the tab should open saying that `You are now authenticated with the gcloud CLI!`

3. Install the Google GenAI <u>SDK</u> [preferential] or  Vertex <u>Generative AI SDK</u>

4. Try running the minimal example

[if you previously chose to use Google GenAI <u>SDK</u> and installed google-genai] `from google import genai`

```
if __name__ == '__main__':
  client = genai.Client(
    vertexai=True,
    project='gd-gcp-gridu-
    genai',
    location='us-central1'
  )

  response = client.models.generate_content(
    model='gemini-2.0-flash-001', contents='Why is sky blue?'
  )

  print(response.text)
```

OR

[if you previously chose to use Vertex Generative AI SDK and installed google-cloud-aiplatform]

```
import vertexai
vertexai.init(project='gd-gcp-gridu-genai', location='us-central1')
if __name__ == '__main__':
  from vertexai.generative_models import GenerativeModel`


  model = GenerativeModel("gemini-2.0-flash-001")
  response = model.generate_content("Why is sky blue?")
  print(response.text)
```

The answer should be a valid Gemini response

### **Quick Fix Guide: Google Cloud SDK Installation Permission Problems**

**Scenario 1: Error writing to** ~/.config/gcloud

**Symptom:** Installation fails with PermissionError: [Errno 13] Permission denied: .../.config/gcloud/config_sentinel .

**Cause:** The ~/.config or ~/.config/gcloud directory is owned by root .

**Verify** ls -ld ~/.config (Look for root as the owner).

**Solution:**

1. Change ownership (replace your_username with your actual username):

`sudo chown -R your_username:staff ~/.config`

2. Retry the SDK installation step.

## Gemini access instructions

**Scenario 2: Failed to update shell file (e.g.,** ~/.zshrc **) &** gcloud: command not found


#### **Symptom:**

- Installer warns: "Could not update /Users/your_username/.zshrc. Ensure you have write access..."

- Later, typing gcloud gives zsh: command not found: gcloud .

**Cause:** Your shell's startup file (e.g., ~/.zshrc , ~/.bash_profile ) is owned by root , so the installer couldn't add gcloud to your PATH.

**Verify** ls -l ~/.zshrc  # Or your shell's specific rc file (Look for root as the owner).

#### **Solution:**

- **Fix ownership** (replace your_username and adjust ~/.zshrc if using a different shell file): sudo chown your_username:staff ~/.zshrc

○ **Manually add SDK paths to the rc file.** _(Assuming your SDK is at_ /Users/your_username/googlecloud-sdk/ _. Adjust path if different.)_ Add these lines to your ~/.zshrc (or equivalent):

```
echo '' >> ~/.zshrc
echo '# Google Cloud SDK Config' >> ~/.zshrc
echo 'export PATH="/Users/your_username/google-cloud-sdk/bin:$PATH"' >> ~/.zshrc
echo 'if [ -f "/Users/your_username/google-cloud-sdk/path.zsh.inc" ]; then . "/Users/your_username/google-cloud-sdk/path.zsh.inc"; fi' >> ~/.zshrc
echo 'if [ -f "/Users/your_username/google-cloud-sdk/completion.zsh.inc" ]; then . "/Users/your_username/google-cloud-sdk/completion.zsh.inc"; fi' >> ~/.zshrc

```

- **Apply changes** to your current terminal session: source ~/.zshrc # Or close and reopen your terminal

- **Test** gcloud
