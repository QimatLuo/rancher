export K3S_KUBECONFIG_MODE="644"
export INSTALL_K3S_EXEC="--disable traefik --disable servicelb"
export K3S_NODE_NAME="rpi5"
curl -sfL https://get.k3s.io | sh -s -
ln -s /etc/rancher/k3s/k3s.yaml ~/.kube/config

sudo cat /var/lib/rancher/k3s/server/node-token
