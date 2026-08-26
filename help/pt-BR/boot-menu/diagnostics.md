# Diagnóstico e opções avançadas

Ajuste o registro de inicialização ou adicione parâmetros não representados pelos controles disponíveis.

## Mensagens de inicialização

**Ocultar mensagens rotineiras de inicialização** adiciona `quiet`. **Ativar registro de diagnóstico** adiciona
`debug`. Eles podem ser ativados independentemente. Desative `quiet` quando mensagens detalhadas de inicialização
forem mais úteis do que uma tela de inicialização limpa.

## Parâmetros adicionais

Insira apenas parâmetros do Linux, initramfs do MiniOS ou live-config que não possuam um
controle tipado. Parâmetros desconhecidos existentes são preservados aqui, e a conclusão
continua sugerindo opções comuns.

Parâmetros específicos da entrada são adicionados após os argumentos do kernel do modelo e globais. Para uma opção MiniOS `key=value` repetida, normalmente o último valor prevalece. Parâmetros de especialista inválidos ou conflitantes podem impedir a inicialização.
