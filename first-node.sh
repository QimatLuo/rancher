export INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --cluster-init"
export K3S_KUBECONFIG_MODE="644"
export K3S_NODE_NAME=""
curl -sfL https://get.k3s.io | sh -s -

sudo cat /var/lib/rancher/k3s/server/node-token
