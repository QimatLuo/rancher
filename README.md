1. Enable ssh on RPi
1. Install k3s on RPi
```bash
ssh -t pi@192.168.1.115 'curl -fsSL https://raw.githubusercontent.com/QimatLuo/rancher/refs/heads/main/k3s.sh | sh'
```
1. Copy kubeconfig from RPi
```bash
scp pi5@192.168.1.115:/etc/rancher/k3s/k3s.yaml "$HOME.kube/config"
``````
1. Replace ip in kubeconfig
```bash
sed -i 's/127.0.0.1/192.168.1.115/' "$HOME.kube/config"
```
