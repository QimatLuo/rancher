ip=$(echo $1 | awk -F'@' '{ print $2 }')
echo $ip
scp "$1:/etc/rancher/k3s/k3s.yaml" "$HOME/.kube/config"
sed -i "s/127.0.0.1/$ip/" "$HOME/.kube/config"
